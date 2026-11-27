"""OpenWeather tool configuration and native implementation."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Literal

import httpx

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import (
    UNSET,
    require_enabled_action,
    require_non_empty,
    resolve_bool_toggle,
    restrict_actions,
)

if TYPE_CHECKING:
    from mindroom.tool_system.tool_function import ToolFunction

logger = get_logger(__name__)

_OpenWeatherAction = Literal["current_weather", "forecast", "air_pollution", "geocode"]
_OPENWEATHER_HOSTS = frozenset({"api.openweathermap.org"})


class _OpenWeatherUrlLogFilter(logging.Filter):
    """Drop the query string (including ``appid``) from httpx request logs for OpenWeather requests."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                arg.copy_with(query=None)
                if isinstance(arg, httpx.URL) and (arg.host in _OPENWEATHER_HOSTS or "appid" in arg.params)
                else arg
                for arg in record.args
            )
        return True


def _install_httpx_log_filter() -> None:
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(log_filter, _OpenWeatherUrlLogFilter) for log_filter in httpx_logger.filters):
        httpx_logger.addFilter(_OpenWeatherUrlLogFilter())


_install_httpx_log_filter()


class _OpenWeatherTools(Toolkit):
    """Fetch current weather, forecasts, air quality, and geocoding from OpenWeatherMap."""

    def __init__(
        self,
        api_key: str | None = None,
        units: str = "metric",
        enable_current_weather: bool | None = UNSET,
        enable_forecast: bool | None = UNSET,
        enable_air_pollution: bool | None = UNSET,
        enable_geocoding: bool | None = UNSET,
        all: bool | None = UNSET,  # noqa: A002 - matches ConfigField name
        timeout: int = 30,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            msg = "OpenWeather API key is required."
            raise ValueError(msg)
        self.api_key = api_key.strip()
        self.units = units
        self.timeout = timeout
        self.base_url = "https://api.openweathermap.org/data/2.5"
        self.geo_url = "https://api.openweathermap.org/geo/1.0"

        enable_all = resolve_bool_toggle(all, default=False)
        flag_map: tuple[tuple[str, bool | None], ...] = (
            ("current_weather", enable_current_weather),
            ("forecast", enable_forecast),
            ("air_pollution", enable_air_pollution),
            ("geocode", enable_geocoding),
        )
        self._enabled_actions = tuple(
            action for action, flag in flag_map if enable_all or resolve_bool_toggle(flag, default=True)
        )
        tools = [self.openweather_get] if self._enabled_actions else []
        super().__init__(name="openweather_tools", tools=tools)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Restrict ``openweather_get`` actions to the ones enabled on this instance."""
        if function.name == "openweather_get":
            return restrict_actions(function, self._enabled_actions)
        return function

    def _request_json(self, url: str, params: dict[str, Any]) -> Any:  # noqa: ANN401 - JSON dict or list
        _install_httpx_log_filter()
        try:
            response = httpx.get(url, params={**params, "appid": self.api_key}, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("openweather_http_error", url=url, status_code=exc.response.status_code)
            return {"error": f"OpenWeather request failed with HTTP {exc.response.status_code}"}
        except (httpx.HTTPError, ValueError) as exc:
            sanitized = str(exc).replace(self.api_key, "[REDACTED]")
            logger.warning("openweather_request_failed", url=url, error=sanitized)
            return {"error": sanitized}

    def _geocode(self, location: str, limit: int = 1) -> list[dict[str, Any]] | dict[str, Any]:
        result = self._request_json(f"{self.geo_url}/direct", {"q": location, "limit": max(1, limit)})
        if isinstance(result, dict) and "error" in result:
            return result
        if not isinstance(result, list) or not result:
            return {"error": f"No location found for '{location}'"}
        return result

    def openweather_get(
        self,
        action: _OpenWeatherAction,
        location: str,
        days: int = 5,
        limit: int = 1,
    ) -> str:
        """Query OpenWeatherMap for `location` (city, optionally with state/country).

        Actions:
        - `current_weather`: current weather conditions.
        - `forecast`: 3-hour forecast entries for `days` (1-5).
        - `air_pollution`: current air pollution and AQI metrics.
        - `geocode`: resolve `location` to up to `limit` coordinate matches.
        """
        if (disabled := require_enabled_action(action, self._enabled_actions)) is not None:
            return disabled
        try:
            place = require_non_empty(location, "location")
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

        geo = self._geocode(place, limit=limit if action == "geocode" else 1)
        if isinstance(geo, dict):
            return json.dumps(geo)
        if action == "geocode":
            return json.dumps(geo, indent=2)

        loc = geo[0]
        coords = {"lat": loc["lat"], "lon": loc["lon"]}
        if action == "current_weather":
            payload = self._request_json(f"{self.base_url}/weather", {**coords, "units": self.units})
        elif action == "forecast":
            clamped_days = max(1, min(days, 5))
            payload = self._request_json(
                f"{self.base_url}/forecast",
                {**coords, "units": self.units, "cnt": clamped_days * 8},
            )
        elif action == "air_pollution":
            payload = self._request_json(f"{self.base_url}/air_pollution", coords)
        else:
            return json.dumps({"error": f"Unsupported action: {action}"})

        if isinstance(payload, dict) and "error" not in payload:
            payload["location_name"] = loc.get("name", place)
            payload["country"] = loc.get("country", "")
        return json.dumps(payload, indent=2)


@register_tool_with_metadata(
    name="openweather",
    file_access=ToolFileAccess.NONE,
    display_name="OpenWeather",
    description="Weather data services from OpenWeatherMap API",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="WiDaySunny",
    icon_color="text-orange-500",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("OPENWEATHER_API_KEY",),
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
        bool_config_field("enable_current_weather", "Enable Current Weather"),
        bool_config_field("enable_forecast", "Enable Forecast"),
        bool_config_field("enable_air_pollution", "Enable Air Pollution"),
        bool_config_field("enable_geocoding", "Enable Geocoding"),
        bool_config_field("all", "All", default=False),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
        ),
    ],
    dependencies=[],
    docs_url="https://docs.mindroom.chat/tools/location-commerce-and-home/#openweather",
    function_names=("openweather_get",),
    action_scoped_grants=("openweather_get",),
)
def openweather_tools() -> type[Toolkit]:
    """Return OpenWeather tools for weather data access."""
    return _OpenWeatherTools
