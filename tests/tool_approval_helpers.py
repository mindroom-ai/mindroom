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
    """Return ``toolkit``'s adapted functions by name, with the approval flags a native-approval agent sets.

    The toolkit is registered as ``registered_tool_name``, or under its own name.
    """
    adapted = apply_tool_approval_capability(
        adapt_toolkit(toolkit),
        toolkit,
        config,
        registered_tool_name=registered_tool_name or toolkit.name,
        approvals_as_jobs=approvals_as_jobs,
    )
    return {function.name: function for function in adapted.functions}
