"""API endpoints for tools information."""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from mindroom.config.main import Config
from mindroom.credentials import CredentialsManager, get_credentials_manager
from mindroom.tools_metadata import ensure_tool_registry_loaded, export_tools_metadata

from .google_tools_helper import check_google_tool_configured

router = APIRouter(prefix="/api/tools", tags=["tools"])


class ToolsResponse(BaseModel):
    """Response containing all registered tools."""

    tools: list[dict]


def _check_homeassistant_configured(tool_name: str, manager: CredentialsManager) -> bool:
    """Check if HomeAssistant is configured."""
    if tool_name == "homeassistant":
        ha_creds = manager.load_credentials("homeassistant")
        if not ha_creds:
            return False
        # Check for the fields that HomeAssistantTools actually uses
        has_url = "instance_url" in ha_creds
        has_token = "access_token" in ha_creds or "long_lived_token" in ha_creds
        return has_url and has_token
    return False


def _check_standard_tool_configured(tool: dict[str, Any], manager: CredentialsManager) -> bool:
    """Check if a standard tool with config_fields is configured."""
    if not tool.get("config_fields"):
        return False

    credentials = manager.load_credentials(tool["name"])
    if not credentials:
        return False

    # Check if all required fields are present
    required_fields = [field["name"] for field in tool.get("config_fields", []) if field.get("required", True)]
    return all(field in credentials for field in required_fields)


@router.get("")
async def get_registered_tools() -> ToolsResponse:
    """Get all registered tools from mindroom.

    This builds tool metadata from the in-memory registry and updates availability
    based on credentials (including plugin-provided tools).
    """
    from mindroom.api.main import load_runtime_config  # noqa: PLC0415

    config, config_path = load_runtime_config()
    ensure_tool_registry_loaded(config, config_path=config_path)
    tools = export_tools_metadata()

    # Append config-only tool presets so the dashboard picker can offer them.
    for preset_name, expansion in Config.TOOL_PRESETS.items():
        tools.append(
            {
                "name": preset_name,
                "display_name": preset_name.replace("_", " ").title(),
                "description": f"Tool preset that expands to: {', '.join(expansion)}.",
                "category": "preset",
                "status": "available",
                "setup_type": "none",
                "icon": "Workflow",
                "icon_color": "text-orange-500",
                "config_fields": None,
                "dependencies": None,
                "auth_provider": None,
                "docs_url": None,
                "helper_text": f"Config-only macro. Expands to: {', '.join(expansion)}.",
            },
        )

    # Get credentials manager to check if tools are configured
    manager = get_credentials_manager()

    # Update status for tools that require configuration
    for tool in tools:
        tool_name = tool["name"]
        if tool.get("status") == "requires_config":
            # Check if tool has delegated auth
            auth_provider = tool.get("auth_provider")
            if auth_provider:
                # Check if the auth provider is configured
                provider_creds = manager.load_credentials(auth_provider)
                if provider_creds and (
                    (auth_provider == "google" and check_google_tool_configured(tool_name, provider_creds))
                    or auth_provider != "google"
                ):
                    tool["status"] = "available"
            # Check other configured tools
            elif _check_homeassistant_configured(tool_name, manager) or _check_standard_tool_configured(tool, manager):
                tool["status"] = "available"

    return ToolsResponse(tools=tools)
