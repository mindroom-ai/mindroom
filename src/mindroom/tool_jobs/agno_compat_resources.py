"""Bind SDK toolkit connect/close lists to the shared resource owner."""

from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING

from agno.agent import _init as agent_init
from agno.team import _init as team_init
from agno.tools import Toolkit

from mindroom.tool_jobs.resources import (
    connect_async_execution_resource,
    connect_execution_resource,
    current_execution_resources,
    disconnect_async_execution_resource,
    disconnect_execution_resource,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from agno.agent import Agent
    from agno.team import Team

type _Actor = Agent | Team
type _Connect[Actor] = Callable[[Actor], None]
type _AsyncConnect[Actor] = Callable[[Actor], Coroutine[object, object, None]]

_INSTALLED = False


def _sync_bindings[Actor: _Actor](
    connect: _Connect[Actor],
    disconnect: _Connect[Actor],
) -> tuple[_Connect[Actor], _Connect[Actor]]:
    def open_tools(actor: Actor) -> None:
        if current_execution_resources() is None:
            connect(actor)
            return
        for toolkit in actor.tools if isinstance(actor.tools, list) else []:
            if not isinstance(toolkit, Toolkit) or not toolkit.requires_connect:
                continue
            initialized = actor._connectable_tools_initialized_on_run or []
            if toolkit in initialized:
                continue
            captured = copy(actor)
            captured.tools = [toolkit]
            captured._connectable_tools_initialized_on_run = []
            connect_execution_resource(
                toolkit,
                lambda captured=captured: connect(captured),
                lambda captured=captured: disconnect(captured),
            )
            initialized.append(toolkit)
            actor._connectable_tools_initialized_on_run = initialized

    def close_tools(actor: Actor) -> None:
        if current_execution_resources() is None:
            disconnect(actor)
            return
        captured = actor._connectable_tools_initialized_on_run or []
        actor._connectable_tools_initialized_on_run = []
        for toolkit in captured:
            disconnect_execution_resource(toolkit)

    return open_tools, close_tools


def _async_bindings[Actor: _Actor](
    connect: _AsyncConnect[Actor],
    disconnect: _AsyncConnect[Actor],
) -> tuple[_AsyncConnect[Actor], _AsyncConnect[Actor]]:
    async def open_tools(actor: Actor) -> None:
        if current_execution_resources() is None:
            await connect(actor)
            return
        for toolkit in actor.tools if isinstance(actor.tools, list) else []:
            if not isinstance(toolkit, Toolkit) or not any(
                base.__name__ == "MCPTools" for base in type(toolkit).__mro__
            ):
                continue
            initialized = actor._mcp_tools_initialized_on_run or []
            if toolkit in initialized:
                continue
            captured = copy(actor)
            captured.tools = [toolkit]
            captured._mcp_tools_initialized_on_run = []
            initialized.append(toolkit)
            actor._mcp_tools_initialized_on_run = initialized
            await connect_async_execution_resource(
                toolkit,
                lambda captured=captured: connect(captured),
                lambda captured=captured: disconnect(captured),
            )

    async def close_tools(actor: Actor) -> None:
        if current_execution_resources() is None:
            await disconnect(actor)
            return
        captured = actor._mcp_tools_initialized_on_run or []
        actor._mcp_tools_initialized_on_run = []
        for toolkit in captured:
            await disconnect_async_execution_resource(toolkit)

    return open_tools, close_tools


# AGNO_COMPAT: SDK finally blocks close run-owned toolkit lists before returning.
# Reason: Agno has no public lease callback for detached application tools.
# Upstream issue: No matching public resource-lease extension point identified.
# Upstream PR: None identified.
# Remove when: SDK connections can be owned through a public run/job lease.
# Coverage: tests/test_tool_job_execution.py and tests/test_tool_job_resources.py.
def install_execution_resource_bindings() -> None:
    """Bind only connection admission and exact toolkit-list teardown ownership."""
    global _INSTALLED
    if _INSTALLED:
        return
    vars(agent_init)["connect_connectable_tools"], vars(agent_init)["disconnect_connectable_tools"] = _sync_bindings(
        agent_init.connect_connectable_tools,
        agent_init.disconnect_connectable_tools,
    )
    vars(team_init)["_connect_connectable_tools"], vars(team_init)["_disconnect_connectable_tools"] = _sync_bindings(
        team_init._connect_connectable_tools,
        team_init._disconnect_connectable_tools,
    )
    vars(agent_init)["connect_mcp_tools"], vars(agent_init)["disconnect_mcp_tools"] = _async_bindings(
        agent_init.connect_mcp_tools,
        agent_init.disconnect_mcp_tools,
    )
    vars(team_init)["_connect_mcp_tools"], vars(team_init)["_disconnect_mcp_tools"] = _async_bindings(
        team_init._connect_mcp_tools,
        team_init._disconnect_mcp_tools,
    )
    _INSTALLED = True
