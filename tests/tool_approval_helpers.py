"""Apply the agent build's approval policy to one toolkit, as ``create_agent`` applies it to the tools it offers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.agents import apply_tool_approval_capability
from mindroom.tool_system.agno_toolkits import adapt_toolkit

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.tool_system.agno_toolkits import Toolkit
    from mindroom.tool_system.tool_function import ToolFunction


def gated_functions(
    toolkit: Toolkit,
    config: Config,
    *,
    registered_tool_name: str | None = None,
    resumable: bool = True,
    approvals_as_jobs: bool = False,
) -> dict[str, ToolFunction]:
    """Return the adapted functions of ``toolkit`` an agent is offered, by name, with the approval flags it sets.

    The toolkit is registered as ``registered_tool_name``, or under its own name. A toolkit named like a tool the gate
    special-cases (``scheduler``, ``delegate``, a Matrix room-runtime tool) therefore gets that tool's rules unless it
    is registered under another name. A caller that cannot resume a paused run (``resumable`` False) is offered none of
    the functions that ask for approval.
    """
    adapted = apply_tool_approval_capability(
        adapt_toolkit(toolkit),
        toolkit,
        config,
        registered_tool_name=registered_tool_name or toolkit.name,
        resumable=resumable,
        approvals_as_jobs=approvals_as_jobs,
    )
    return {function.name: function for function in adapted.functions}
