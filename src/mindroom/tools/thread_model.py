"""Thread model switching tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.thread_model import ThreadModelTools


@register_tool_with_metadata(
    name="thread_model",
    display_name="Thread Model",
    description="Switch which configured model the current Matrix thread uses",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Cpu",
    icon_color="text-purple-500",
    dependencies=["agno"],
    docs_url="https://github.com/mindroom-ai/mindroom",
    function_names=("get_thread_model", "switch_thread_model", "reset_thread_model"),
)
def thread_model_tools() -> type[ThreadModelTools]:
    """Return per-thread model switching tools."""
    from mindroom.custom_tools.thread_model import ThreadModelTools

    return ThreadModelTools
