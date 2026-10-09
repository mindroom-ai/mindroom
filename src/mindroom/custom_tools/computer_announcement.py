"""Show the agent's worker computer in MindRoom Chat the first time its browser runs there."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from mindroom.custom_tools.chat_ui import show_computer_once
from mindroom.logging_config import get_logger
from mindroom.orchestration.computer_runtime import computer_browser_provider
from mindroom.runtime_env_policy import WORKER_COMPUTER_ENABLED_ENV
from mindroom.tool_system.declarations import SupportsPrimaryCallPlacement
from mindroom.worker_computer.sessions import ComputerError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agno.tools import Toolkit

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


def attach_computer_announcement(
    toolkit: Toolkit,
    tool_name: str,
    *,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> Toolkit:
    """Announce the computer before calls of the browser tool that gives a chat_ui agent its worker computer.

    The hook goes after the existing hooks, so it runs inside the tool hook bridge and only for
    calls that plugin before-call hooks let through.
    """
    if (
        tool_name not in {"browser", "browser_mcp"}
        or not runtime_paths.env_flag(WORKER_COMPUTER_ENABLED_ENV)
        or "chat_ui" not in config.resolve_entity(agent_name).available_tools
    ):
        return toolkit
    try:
        if computer_browser_provider(agent_name, config, runtime_paths) != tool_name:
            return toolkit
    except ComputerError:
        return toolkit
    announce = _announcement_hook(toolkit if isinstance(toolkit, SupportsPrimaryCallPlacement) else None)
    for function in (*toolkit.functions.values(), *toolkit.async_functions.values()):
        hooks = list(function.tool_hooks or [])
        if announce not in hooks:
            function.tool_hooks = [*hooks, announce]
    return toolkit


def _announcement_hook(placement: SupportsPrimaryCallPlacement | None) -> Callable[..., Awaitable[object]]:
    async def announce_computer(name: str, func: Callable[..., object], args: dict[str, Any]) -> object:
        try:
            # Desktop browser calls stay on the primary and drive the user's own browser, not the computer.
            if placement is None or not placement.runs_on_primary(name, args):
                await show_computer_once()
        except Exception:
            logger.warning("Could not announce the worker computer in MindRoom Chat", tool=name, exc_info=True)
        result = func(**args)
        return await result if inspect.isawaitable(result) else result

    return announce_computer
