"""OpenWeather tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.openweather import OpenWeatherTools


@register_tool_with_metadata(
    name="openweather",
    display_name="OpenWeather",
    description="Weather data services from OpenWeatherMap API",
    category=ToolCategory.DEVELOPMENT,  # Based on agno docs structure: /tools/toolkits/others/
    status=ToolStatus.REQUIRES_CONFIG,  # Requires API key
    setup_type=SetupType.API_KEY,  # Uses api_key parameter
    icon="WiDaySunny",  # Weather icon
    icon_color="text-orange-500",  # Orange sun color
    config_fields=[
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="units",
            label="Units",
            type="text",
            required=False,
            default="metric",
        ),
        ConfigField(
            name="enable_current_weather",
            label="Enable Current Weather",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_forecast",
            label="Enable Forecast",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_air_pollution",
            label="Enable Air Pollution",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_geocoding",
            label="Enable Geocoding",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["requests"],  # From agno requirements
    docs_url="https://docs.agno.com/tools/toolkits/others/openweather",  # URL without .md extension
)
def openweather_tools() -> type[OpenWeatherTools]:
    """Return OpenWeather tools for weather data access."""
    from agno.tools.openweather import OpenWeatherTools

    return OpenWeatherTools
