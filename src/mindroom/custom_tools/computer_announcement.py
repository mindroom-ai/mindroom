"""Show the agent's worker computer in MindRoom Chat the first time its browser runs there."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.custom_tools.chat_ui import show_computer_once
from mindroom.logging_config import get_logger
from mindroom.orchestration.computer_runtime import computer_browser_provider
from mindroom.runtime_env_policy import WORKER_COMPUTER_ENABLED_ENV
from mindroom.tool_system.declarations import SupportsPrimaryCallPlacement
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.worker_computer.sessions import ComputerError

if TYPE_CHECKING:
    from agno.tools import Toolkit

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
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

    None for any other toolkit or agent. The loop runs it after the tool hook bridge, so it announces only calls that
    plugin before-call hooks let through.
    """
    if (
        tool_name not in {"browser", "browser_mcp"}
        or not runtime_paths.env_flag(WORKER_COMPUTER_ENABLED_ENV)
        or "chat_ui" not in config.resolve_entity(agent_name).available_tools
    ):
        return None
    try:
        if computer_browser_provider(agent_name, config, runtime_paths) != tool_name:
            return None
    except ComputerError:
        return None
    return _announcement_middleware(
        agent_name,
        frozenset((*toolkit.functions, *toolkit.async_functions)),
        toolkit if isinstance(toolkit, SupportsPrimaryCallPlacement) else None,
    )


def _uses_computer(name: str, args: dict[str, Any], placement: SupportsPrimaryCallPlacement | None) -> bool:
    if name == "browser_control":
        # Imported here, not at module level, so that loading agents does not load Playwright.
        from mindroom.custom_tools.browser import is_reference_action  # noqa: PLC0415

        # help and actions only return reference data, so they must not use up the announcement.
        if is_reference_action(args.get("action")):
            return False
    # Desktop browser calls stay on the primary and drive the user's own browser, not the computer.
    return placement is None or not placement.runs_on_primary(name, args)


def _runs_as(agent_name: str) -> bool:
    # A team member's browser runs under the response owner's context, and the notice would name the owner.
    context = get_tool_runtime_context()
    return context is None or context.agent_name == agent_name


def _announcement_middleware(
    agent_name: str,
    function_names: frozenset[str],
    placement: SupportsPrimaryCallPlacement | None,
) -> ToolMiddleware:
    async def announce_computer(call: ToolCall, call_next: ToolNext) -> object:
        name = call.function.name
        if name in function_names:
            try:
                if _runs_as(agent_name) and _uses_computer(name, call.arguments, placement):
                    await show_computer_once()
            except Exception:
                logger.warning("Could not announce the worker computer in MindRoom Chat", tool=name, exc_info=True)
        return await call_next(call.arguments)

    return announce_computer
