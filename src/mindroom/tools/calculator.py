"""Calculator tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.calculator import CalculatorTools


@register_tool_with_metadata(
    name="calculator",
    display_name="Calculator",
    description="Mathematical calculator with basic and advanced operations",
    category=ToolCategory.DEVELOPMENT,  # Local tool
    status=ToolStatus.AVAILABLE,  # No config needed
    setup_type=SetupType.NONE,  # No authentication required
    icon="Calculator",  # React icon name
    icon_color="text-blue-500",  # Tailwind color class
    config_fields=[],
    dependencies=["agno"],  # From agno requirements
    docs_url="https://docs.agno.com/tools/toolkits/local/calculator",
)
def calculator_tools() -> type[CalculatorTools]:
    """Return calculator tools for mathematical operations."""
    from agno.tools.calculator import CalculatorTools

    return CalculatorTools
