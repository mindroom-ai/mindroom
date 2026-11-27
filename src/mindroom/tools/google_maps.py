"""Google Maps places, geocoding, elevation, timezone, and routing toolkit."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

from mindroom.failure_text import failure_text
from mindroom.redaction import redact_sensitive_text
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
    UnsetType,
    error_json,
    json_dumps,
    require_non_empty,
    resolve_toggle,
    restrict_actions,
)

if TYPE_CHECKING:
    from mindroom.tool_system.tool_function import ToolFunction

_PlacesAction = Literal["search_places", "geocode", "reverse_geocode", "validate_address", "elevation", "timezone"]
_RoutesAction = Literal["directions", "distance_matrix"]
_PLACES_ACTION_FIELDS: dict[str, str] = {
    "search_places": "query",
    "geocode": "address region",
    "reverse_geocode": "lat lng result_type location_type",
    "validate_address": "address region locality enable_usps_cass",
    "elevation": "lat lng",
    "timezone": "lat lng timestamp",
}
_ROUTES_ACTION_FIELDS: dict[str, str] = {
    "directions": "origin destination",
    "distance_matrix": "origins destinations",
}
_TravelMode = Literal["driving", "walking", "bicycling", "transit"]
_GOOGLE_MAPS_BASE_URL = "https://maps.googleapis.com"
_GOOGLE_PLACES_UNIVERSE = "googleapis.com"
_DISTANCE_MATRIX_AVOID = frozenset({"tolls", "highways", "ferries"})
_KEY_QUERY_RE = re.compile(r"([?&]key=)[^&\s\"\x27)]+")


def _normalize_distance_matrix_avoid(avoid: list[str] | str | None) -> str | None:
    if avoid is None:
        return None
    items = [avoid] if isinstance(avoid, str) else avoid
    cleaned = [item.strip().lower() for item in items if isinstance(item, str) and item.strip()]
    if not cleaned:
        return None
    if len(cleaned) > 1:
        msg = "distance_matrix supports at most one avoid restriction ('tolls', 'highways', or 'ferries')."
        raise ValueError(msg)
    if cleaned[0] not in _DISTANCE_MATRIX_AVOID:
        msg = f"Invalid distance_matrix avoid value {cleaned[0]!r}; expected 'tolls', 'highways', or 'ferries'."
        raise ValueError(msg)
    return cleaned[0]


def _parse_optional_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _require_coords(lat: float | None, lng: float | None) -> tuple[float, float]:
    if lat is None or lng is None:
        msg = "Both lat and lng are required."
        raise ValueError(msg)
    return (float(lat), float(lng))


class _GoogleMapTools(Toolkit):
    """Query Google Maps Places, Geocoding, Address Validation, Elevation, Timezone, and Routes APIs."""

    def __init__(
        self,
        key: str | None = None,
        search_places: bool | None | UnsetType = UNSET,
        get_directions: bool | None | UnsetType = UNSET,
        validate_address: bool | None | UnsetType = UNSET,
        geocode_address: bool | None | UnsetType = UNSET,
        reverse_geocode: bool | None | UnsetType = UNSET,
        get_distance_matrix: bool | None | UnsetType = UNSET,
        get_elevation: bool | None | UnsetType = UNSET,
        get_timezone: bool | None | UnsetType = UNSET,
    ) -> None:
        self.api_key = require_non_empty(key, "Google Maps API key")

        import googlemaps

        self.client: Any = googlemaps.Client(key=self.api_key, base_url=_GOOGLE_MAPS_BASE_URL)
        self._places_client: Any = None

        self._places_actions: tuple[_PlacesAction, ...] = tuple(
            action
            for action, value in (
                ("search_places", search_places),
                ("geocode", geocode_address),
                ("reverse_geocode", reverse_geocode),
                ("validate_address", validate_address),
                ("elevation", get_elevation),
                ("timezone", get_timezone),
            )
            if resolve_toggle(value, default=True)
        )
        self._routes_actions: tuple[_RoutesAction, ...] = tuple(
            action
            for action, value in (("directions", get_directions), ("distance_matrix", get_distance_matrix))
            if resolve_toggle(value, default=True)
        )

        tools: list[Any] = []
        if self._places_actions:
            tools.append(self.google_maps_places)
        if self._routes_actions:
            tools.append(self.google_maps_routes)
        super().__init__(name="google_maps", tools=tools)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Restrict Google Maps action schemas and descriptions to enabled actions."""
        if function.name == "google_maps_places":
            return restrict_actions(function, self._places_actions, action_fields=_PLACES_ACTION_FIELDS)
        if function.name == "google_maps_routes":
            return restrict_actions(
                function,
                self._routes_actions,
                action_fields=_ROUTES_ACTION_FIELDS,
                common_fields=("mode", "departure_time", "avoid"),
            )
        return function

    def _get_places_client(self) -> Any:  # noqa: ANN401
        if self._places_client is None:
            from google.maps import places_v1

            self._places_client = places_v1.PlacesClient(
                client_options={
                    "api_key": self.api_key,
                    "api_endpoint": places_v1.PlacesClient.DEFAULT_ENDPOINT,
                    "universe_domain": _GOOGLE_PLACES_UNIVERSE,
                },
            )
        return self._places_client

    def _sanitize_error(self, exc: Exception) -> str:
        text = failure_text(exc)
        for secret in (self.api_key, quote(self.api_key, safe="")):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        text = _KEY_QUERY_RE.sub(r"\1[REDACTED]", text)
        return redact_sensitive_text(text)

    def _search_places(self, query: str) -> list[dict[str, Any]]:
        from google.maps import places_v1

        request = places_v1.SearchTextRequest(text_query=query)
        response = self._get_places_client().search_text(request=request, metadata=[("x-goog-fieldmask", "*")])
        return [
            {
                "name": place.display_name.text,
                "address": place.formatted_address,
                "rating": place.rating,
                "reviews": [{"text": r.text.text, "rating": r.rating} for r in place.reviews],
                "place_id": place.id,
                "phone": place.international_phone_number,
                "website": place.website_uri,
                "hours": list(place.regular_opening_hours.weekday_descriptions),
            }
            for place in response.places
        ]

    def _dispatch_places(
        self,
        action: _PlacesAction,
        query: str | None,
        address: str | None,
        lat: float | None,
        lng: float | None,
        region: str | None,
        locality: str | None,
        enable_usps_cass: bool,
        result_type: list[str] | None,
        location_type: list[str] | None,
        timestamp: str | None,
    ) -> Any:  # noqa: ANN401
        if action == "search_places":
            result: Any = self._search_places(require_non_empty(query, "query"))
        elif action == "geocode":
            result = self.client.geocode(require_non_empty(address, "address"), region=region)
        elif action == "reverse_geocode":
            result = self.client.reverse_geocode(
                _require_coords(lat, lng),
                result_type=result_type,
                location_type=location_type,
            )
        elif action == "validate_address":
            result = self.client.addressvalidation(
                [require_non_empty(address, "address")],
                regionCode=region or "US",
                locality=locality,
                enableUspsCass=enable_usps_cass,
            )
        elif action == "elevation":
            result = self.client.elevation(_require_coords(lat, lng))
        else:
            ts = _parse_optional_datetime(timestamp) or datetime.now(UTC)
            result = self.client.timezone(location=_require_coords(lat, lng), timestamp=ts)
        return result

    def google_maps_places(
        self,
        action: _PlacesAction,
        query: str | None = None,
        address: str | None = None,
        lat: float | None = None,
        lng: float | None = None,
        region: str | None = None,
        locality: str | None = None,
        enable_usps_cass: bool = False,
        result_type: list[str] | None = None,
        location_type: list[str] | None = None,
        timestamp: str | None = None,
    ) -> str:
        """Search places, geocode/validate addresses, or look up elevation and timezone.

        Actions:
        - `search_places`: Text place search (`query`).
        - `geocode`: Convert `address` to coordinates (optional `region`).
        - `reverse_geocode`: Convert `lat`/`lng` to address (optional `result_type`, `location_type`).
        - `validate_address`: Validate `address` (optional `region`, `locality`, `enable_usps_cass`).
        - `elevation`: Elevation at `lat`/`lng`.
        - `timezone`: Timezone at `lat`/`lng` (optional ISO-8601 `timestamp`).
        """
        if action not in self._places_actions:
            return error_json(f"Google Maps action '{action}' is disabled.")
        try:
            return json_dumps(
                self._dispatch_places(
                    action,
                    query,
                    address,
                    lat,
                    lng,
                    region,
                    locality,
                    enable_usps_cass,
                    result_type,
                    location_type,
                    timestamp,
                ),
            )
        except Exception as exc:
            return error_json(self._sanitize_error(exc))

    def google_maps_routes(
        self,
        action: _RoutesAction,
        origin: str | None = None,
        destination: str | None = None,
        origins: list[str] | None = None,
        destinations: list[str] | None = None,
        mode: _TravelMode = "driving",
        departure_time: str | None = None,
        avoid: list[str] | None = None,
    ) -> str:
        """Compute Google Maps directions or distance matrices.

        Actions:
        - `directions`: Route from `origin` to `destination` (`mode`, optional ISO-8601 `departure_time`, `avoid`).
        - `distance_matrix`: Travel time/distance between `origins` and `destinations` (`mode`, `departure_time`, `avoid`).
        """
        if action not in self._routes_actions:
            return error_json(f"Google Maps action '{action}' is disabled.")
        try:
            dep = _parse_optional_datetime(departure_time)
            if action == "directions":
                data = self.client.directions(
                    require_non_empty(origin, "origin"),
                    require_non_empty(destination, "destination"),
                    mode=mode,
                    departure_time=dep,
                    avoid=avoid or None,
                )
            else:
                if not origins or not destinations:
                    return error_json("Both origins and destinations must be non-empty lists.")
                data = self.client.distance_matrix(
                    origins,
                    destinations,
                    mode=mode,
                    departure_time=dep,
                    avoid=_normalize_distance_matrix_avoid(avoid),
                )
            return json_dumps(data)
        except Exception as exc:
            return error_json(self._sanitize_error(exc))


@register_tool_with_metadata(
    name="google_maps",
    file_access=ToolFileAccess.NONE,
    display_name="Google Maps",
    description="Tools for interacting with Google Maps services including place search, directions, geocoding, and more",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiGooglemaps",
    icon_color="text-red-500",
    config_fields=[
        ConfigField(
            name="key",
            env=("GOOGLE_MAPS_API_KEY",),
            label="Key",
            type="password",
            required=False,
            default=None,
        ),
        bool_config_field("search_places", "Search Places"),
        bool_config_field("get_directions", "Get Directions"),
        bool_config_field("validate_address", "Validate Address"),
        bool_config_field("geocode_address", "Geocode Address"),
        bool_config_field("reverse_geocode", "Reverse Geocode"),
        bool_config_field("get_distance_matrix", "Get Distance Matrix"),
        bool_config_field("get_elevation", "Get Elevation"),
        bool_config_field("get_timezone", "Get Timezone"),
    ],
    dependencies=["googlemaps", "google-maps-places"],
    docs_url="https://docs.mindroom.chat/tools/location-commerce-and-home/#google_maps",
    function_names=("google_maps_places", "google_maps_routes"),
    action_scoped_grants=("google_maps_places", "google_maps_routes"),
)
def google_maps_tools() -> type[Toolkit]:
    """Return Google Maps tools for location services."""
    return _GoogleMapTools
