"""Apply the agent build's approval policy to one toolkit, as ``create_agent`` applies it to the tools it offers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

from mindroom.agents import apply_tool_approval_capability
from mindroom.approval import manager as approval_manager
from mindroom.tool_system.agno_toolkits import adapt_toolkit

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from mindroom.config.main import Config
    from mindroom.tool_system.agno_toolkits import Toolkit
    from mindroom.tool_system.tool_function import ToolFunction


def gated_functions(
    toolkit: Toolkit,
    config: Config,
    *,
    registered_tool_name: str | None = None,
    authored_as: str | None = None,
    resumable: bool = True,
    approvals_as_jobs: bool = False,
) -> dict[str, ToolFunction]:
    """Return the adapted functions of ``toolkit`` an agent is offered, by name, with the approval flags it sets.

    The toolkit is registered as ``registered_tool_name``, or under its own name, and authored as ``authored_as``, or
    under its registered name. A toolkit named like a tool the gate special-cases (``scheduler``, ``delegate``, a
    Matrix room-runtime tool) therefore gets that tool's rules unless it is registered under another name. A caller
    that cannot resume a paused run (``resumable`` False) is offered none of the functions that ask for approval.
    """
    registered = registered_tool_name or toolkit.name
    adapted = apply_tool_approval_capability(
        adapt_toolkit(toolkit, key=authored_as or registered),
        toolkit,
        config,
        registered_tool_name=registered,
        resumable=resumable,
        approvals_as_jobs=approvals_as_jobs,
    )
    return {function.name: function for function in adapted.functions}


def scheduled_call_approvals(**methods: object) -> AbstractContextManager[object]:
    """Stand in for the approval manager with the scheduled-call methods a test names, as scheduling reaches it."""
    return patch.object(approval_manager, "get_approval_store", return_value=SimpleNamespace(**methods))
