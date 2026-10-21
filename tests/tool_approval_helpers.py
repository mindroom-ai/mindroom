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
    approvals_as_jobs: bool = False,
) -> dict[str, ToolFunction]:
    """Return ``toolkit``'s adapted functions by name, with the approval flags a native-approval agent sets."""
    adapted = apply_tool_approval_capability(
        adapt_toolkit(toolkit),
        toolkit,
        config,
        registered_tool_name=registered_tool_name,
        approvals_as_jobs=approvals_as_jobs,
    )
    return {function.name: function for function in adapted.functions}


def with_agno_approval_flags(
    toolkit: Toolkit,
    config: Config,
    *,
    registered_tool_name: str | None = None,
    approvals_as_jobs: bool = False,
) -> Toolkit:
    """Copy those flags onto ``toolkit``'s Agno functions, for the runners that still dispatch Agno functions."""
    gated = gated_functions(
        toolkit,
        config,
        registered_tool_name=registered_tool_name,
        approvals_as_jobs=approvals_as_jobs,
    )
    for function in (*toolkit.functions.values(), *toolkit.async_functions.values()):
        flagged = gated[function.name]
        function.requires_confirmation = flagged.requires_confirmation
        function.approval_type = flagged.approval_type
        function.external_execution = flagged.external_execution
        function.external_execution_silent = flagged.external_execution_silent
    return toolkit
