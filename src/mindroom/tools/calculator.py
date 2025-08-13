"""Calculator tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
    ConfigField,
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
    config_fields=[
        # Basic arithmetic operations (enabled by default)
        ConfigField(
            name="add",
            label="Addition",
            type="boolean",
            required=False,
            default=True,
            description="Enable addition operation",
        ),
        ConfigField(
            name="subtract",
            label="Subtraction",
            type="boolean",
            required=False,
            default=True,
            description="Enable subtraction operation",
        ),
        ConfigField(
            name="multiply",
            label="Multiplication",
            type="boolean",
            required=False,
            default=True,
            description="Enable multiplication operation",
        ),
        ConfigField(
            name="divide",
            label="Division",
            type="boolean",
            required=False,
            default=True,
            description="Enable division operation",
        ),
        # Advanced mathematical operations (disabled by default)
        ConfigField(
            name="exponentiate",
            label="Exponentiation",
            type="boolean",
            required=False,
            default=False,
            description="Enable exponentiation operation (power calculation)",
        ),
        ConfigField(
            name="factorial",
            label="Factorial",
            type="boolean",
            required=False,
            default=False,
            description="Enable factorial calculation for integers",
        ),
        ConfigField(
            name="is_prime",
            label="Prime Check",
            type="boolean",
            required=False,
            default=False,
            description="Enable prime number checking",
        ),
        ConfigField(
            name="square_root",
            label="Square Root",
            type="boolean",
            required=False,
            default=False,
            description="Enable square root calculation",
        ),
        # Global control
        ConfigField(
            name="enable_all",
            label="Enable All Operations",
            type="boolean",
            required=False,
            default=False,
            description="Enable all available mathematical operations regardless of individual settings",
        ),
    ],
    dependencies=["agno"],  # From agno requirements
    docs_url="https://docs.agno.com/tools/toolkits/local/calculator",
)
def calculator_tools() -> type[CalculatorTools]:
    """Return calculator tools for mathematical operations."""
    from agno.tools.calculator import CalculatorTools

    return CalculatorTools
