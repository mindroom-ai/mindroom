"""Reasoning tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.reasoning import ReasoningTools


@register_tool_with_metadata(
    name="reasoning",
    display_name="Reasoning",
    description="Step-by-step reasoning scratchpad with think and analyze tools for structured problem solving",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaBrain",
    icon_color="text-purple-500",
    config_fields=[
        ConfigField(
            name="enable_think",
            label="Enable Think",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_analyze",
            label="Enable Analyze",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="add_instructions",
            label="Add Instructions",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="add_few_shot",
            label="Add Few-Shot Examples",
            type="boolean",
            required=False,
            default=False,
        ),
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
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=[],
    docs_url="https://docs.agno.com/tools/toolkits/others/reasoning",
)
def reasoning_tools() -> type[ReasoningTools]:
    """Return Reasoning tools for step-by-step problem solving."""
    from agno.tools.reasoning import ReasoningTools

    return ReasoningTools
