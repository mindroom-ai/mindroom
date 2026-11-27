"""Show the agent's worker computer in MindRoom Chat the first time its browser runs there."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.browser.call_limit import BROWSER_READ_PAGE_ACTIONS
from mindroom.custom_tools.chat_ui import show_computer_once
from mindroom.logging_config import get_logger
from mindroom.orchestration.computer_runtime import worker_computer_provider
from mindroom.tool_system.declarations import SupportsPrimaryCallPlacement
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.authoring import Toolkit
    from mindroom.tool_system.invocation import ToolCall, ToolMiddleware, ToolNext

logger = get_logger(__name__)


def computer_announcement_middleware(
    toolkit: Toolkit,
    tool_name: str,
    *,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> ToolMiddleware | None:
    """Return the middleware announcing the computer before the browser tool that gives a chat_ui agent its computer.

    None for any other toolkit or agent. Each of the toolkit's functions carries it as its own policy, which runs
    inside the tool hook bridge, so it announces only calls that plugin before-call hooks let through.
    """
    if (
        tool_name not in {"browser", "browser_mcp"}
        or "chat_ui" not in config.resolve_agent(agent_name).tools.tool_names
        or worker_computer_provider(agent_name, config, runtime_paths) != tool_name
    ):
        return None
    placement = toolkit if isinstance(toolkit, SupportsPrimaryCallPlacement) else None

    async def announce_computer(call: ToolCall, call_next: ToolNext) -> object:
        name = call.function.name
        should_announce = False
        try:
            should_announce = _runs_as(agent_name) and _uses_computer(name, call.arguments, placement)
            if should_announce and name != "browser_read":
                await show_computer_once()
        except Exception:
            logger.warning("Could not announce the worker computer in MindRoom Chat", tool=name, exc_info=True)
        result = await call_next(call.arguments)
        # browser_read page views fail when Chromium is stopped, so announce only after one succeeds.
        if should_announce and name == "browser_read":
            try:
                await show_computer_once()
            except Exception:
                logger.warning("Could not announce the worker computer in MindRoom Chat", tool=name, exc_info=True)
        return result

    return announce_computer


def _uses_computer(name: str, args: dict[str, Any], placement: SupportsPrimaryCallPlacement | None) -> bool:
    # Non-page browser_read queries (status, profiles, tabs, console, help, actions) never launch or show a page.
    if name == "browser_read" and str(args.get("action")).strip().lower() not in BROWSER_READ_PAGE_ACTIONS:
        return False
    # Desktop browser calls stay on the primary and drive the user's own browser, not the computer.
    return placement is None or not placement.runs_on_primary(name, args)


def _runs_as(agent_name: str) -> bool:
    # A team member's browser runs under the response owner's context, and the notice would name the owner.
    context = get_tool_runtime_context()
    return context is None or context.agent_name == agent_name
