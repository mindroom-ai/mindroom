"""Home Assistant tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.homeassistant import HomeAssistantTools


@register_tool_with_metadata(
    name="homeassistant",
    file_access=ToolFileAccess.NONE,
    display_name="Home Assistant",
    description="Control and monitor smart home devices",
    category=ToolCategory.SMART_HOME,
    icon="Home",
    icon_color="text-blue-500",
    dependencies=["httpx"],
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    requires_primary_runtime=True,
    managed_init_args=(
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
    ),
    config_fields=[
        ConfigField(
            name="HOMEASSISTANT_URL",
            label="Home Assistant URL",
            type="url",
            required=True,
            placeholder="http://homeassistant.local:8123",
            description="URL to your Home Assistant instance",
        ),
        ConfigField(
            name="HOMEASSISTANT_TOKEN",
            label="Access Token",
            type="password",
            required=True,
            placeholder="Bearer token",
            description="Long-lived access token from Home Assistant",
        ),
        ConfigField(
            name="HOMEASSISTANT_ALLOW_PRIVATE_URL",
            label="Allow Private Home Assistant URL",
            type="boolean",
            required=False,
            default=False,
            description="Allow a trusted self-hosted Home Assistant URL on a private, local, or loopback network.",
        ),
    ],
    docs_url="https://docs.mindroom.chat/tools/location-commerce-and-home/#homeassistant",
    function_names=(
        "activate_scene",
        "call_service",
        "get_entity_state",
        "list_entities",
        "set_brightness",
        "set_color",
        "set_temperature",
        "toggle",
        "trigger_automation",
        "turn_off",
        "turn_on",
    ),
)
def homeassistant_tools() -> type[HomeAssistantTools]:
    """Return Home Assistant tools for smart home control."""
    from mindroom.custom_tools.homeassistant import HomeAssistantTools

    return HomeAssistantTools
