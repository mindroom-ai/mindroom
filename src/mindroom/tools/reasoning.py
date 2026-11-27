"""Reasoning tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.reasoning import ReasoningTools


@register_tool_with_metadata(
    name="reasoning",
    file_access=ToolFileAccess.NONE,
    requires_primary_runtime=True,
    display_name="Reasoning",
    description="Step-by-step reasoning scratchpad with think and analyze tools for structured problem solving",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaBrain",
    icon_color="text-purple-500",
    config_fields=[
        bool_config_field("enable_think", "Enable Think"),
        bool_config_field("enable_analyze", "Enable Analyze"),
        bool_config_field("add_few_shot", "Add Few-Shot Examples", default=False),
        ConfigField(
            name="instructions",
            label="Instructions",
            type="text",
            required=False,
            default=None,
            description="Custom instructions to override the default reasoning instructions",
        ),
        ConfigField(
            name="few_shot_examples",
            label="Few-Shot Examples",
            type="text",
            required=False,
            default=None,
            description="Custom few-shot examples for the reasoning tools",
        ),
        bool_config_field("all", "All", default=False),
    ],
    dependencies=[],
    docs_url="https://docs.mindroom.chat/tools/execution-and-coding/#reasoning",
    function_names=("analyze", "think"),
)
def reasoning_tools() -> type[ReasoningTools]:
    """Return Reasoning tools for step-by-step problem solving."""
    from mindroom.custom_tools.reasoning import ReasoningTools

    return ReasoningTools
