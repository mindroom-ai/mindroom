"""Session orchestration toolkit configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.session_orchestration import SessionOrchestrationTools


@register_tool_with_metadata(
    name="session_orchestration",
    display_name="Session Orchestration",
    description="Spawn, steer, and inspect Matrix session/subagent runs across agents",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Workflow",
    icon_color="text-teal-500",
    dependencies=["agno"],
    docs_url="https://github.com/mindroom-ai/mindroom",
)
def session_orchestration_tools() -> type[SessionOrchestrationTools]:
    """Return session orchestration tools."""
    from mindroom.custom_tools.session_orchestration import SessionOrchestrationTools

    return SessionOrchestrationTools
