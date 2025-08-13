"""OpenWeather tool configuration."""

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
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="your_openweather_api_key",
            description="OpenWeatherMap API key. If not provided, uses OPENWEATHER_API_KEY env var.",
        ),
        # Configuration
        ConfigField(
            name="units",
            label="Units",
            type="text",
            required=False,
            default="metric",
            placeholder="metric",
            description="Units of measurement. Options: 'standard', 'metric', 'imperial'.",
        ),
        # Feature flags
        ConfigField(
            name="current_weather",
            label="Current Weather",
            type="boolean",
            required=False,
            default=True,
            description="Enable current weather function",
        ),
        ConfigField(
            name="forecast",
            label="Forecast",
            type="boolean",
            required=False,
            default=True,
            description="Enable forecast function",
        ),
        ConfigField(
            name="air_pollution",
            label="Air Pollution",
            type="boolean",
            required=False,
            default=True,
            description="Enable air pollution function",
        ),
        ConfigField(
            name="geocoding",
            label="Geocoding",
            type="boolean",
            required=False,
            default=True,
            description="Enable geocoding function",
        ),
    ],
    dependencies=["requests"],  # From agno requirements
    docs_url="https://docs.agno.com/tools/toolkits/others/openweather",  # URL without .md extension
)
def openweather_tools() -> type[OpenWeatherTools]:
    """Return OpenWeather tools for weather data access."""
    from agno.tools.openweather import OpenWeatherTools

    return OpenWeatherTools
