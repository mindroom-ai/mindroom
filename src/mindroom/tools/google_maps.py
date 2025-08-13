"""Google Maps tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.google_maps import GoogleMapTools


@register_tool_with_metadata(
    name="google_maps",
    display_name="Google Maps",
    description="Tools for interacting with Google Maps services including place search, directions, geocoding, and more",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaMapMarkedAlt",
    icon_color="text-red-500",
    config_fields=[
        # Authentication
        ConfigField(
            name="key",
            label="API Key",
            type="password",
            required=False,
            placeholder="AIza...",
            description="Google Maps API key (can also be set via GOOGLE_MAPS_API_KEY env var)",
        ),
        # Place search functionality
        ConfigField(
            name="search_places",
            label="Search Places",
            type="boolean",
            required=False,
            default=True,
            description="Enable places search functionality",
        ),
        # Directions functionality
        ConfigField(
            name="get_directions",
            label="Get Directions",
            type="boolean",
            required=False,
            default=True,
            description="Enable directions functionality",
        ),
        # Address validation functionality
        ConfigField(
            name="validate_address",
            label="Validate Address",
            type="boolean",
            required=False,
            default=True,
            description="Enable address validation functionality",
        ),
        # Geocoding functionality
        ConfigField(
            name="geocode_address",
            label="Geocode Address",
            type="boolean",
            required=False,
            default=True,
            description="Enable geocoding functionality",
        ),
        # Reverse geocoding functionality
        ConfigField(
            name="reverse_geocode",
            label="Reverse Geocode",
            type="boolean",
            required=False,
            default=True,
            description="Enable reverse geocoding functionality",
        ),
        # Distance matrix functionality
        ConfigField(
            name="get_distance_matrix",
            label="Get Distance Matrix",
            type="boolean",
            required=False,
            default=True,
            description="Enable distance matrix functionality",
        ),
        # Elevation functionality
        ConfigField(
            name="get_elevation",
            label="Get Elevation",
            type="boolean",
            required=False,
            default=True,
            description="Enable elevation functionality",
        ),
        # Timezone functionality
        ConfigField(
            name="get_timezone",
            label="Get Timezone",
            type="boolean",
            required=False,
            default=True,
            description="Enable timezone functionality",
        ),
    ],
    dependencies=["googlemaps", "google-maps-places"],
    docs_url="https://docs.agno.com/tools/toolkits/others/google_maps",
)
def google_maps_tools() -> type[GoogleMapTools]:
    """Return Google Maps tools for location services."""
    from agno.tools.google_maps import GoogleMapTools

    return GoogleMapTools
