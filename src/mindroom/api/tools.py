"""API endpoints for tools information."""

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from mindroom.api.credentials import (
    dashboard_supports_worker_credentials,
    load_credentials_for_target,
    resolve_request_credentials_target,
)
from mindroom.api.google_tools_helper import check_google_tool_configured
from mindroom.config.main import Config
from mindroom.tool_system.metadata import ensure_tool_registry_loaded, export_tools_metadata
from mindroom.tool_system.worker_routing import (
    SHARED_ONLY_INTEGRATION_NAMES,
    WorkerScope,
    worker_scope_allows_shared_only_integrations,
)

router = APIRouter(prefix="/api/tools", tags=["tools"])


class ToolsResponse(BaseModel):
    """Response containing all registered tools."""

    tools: list[dict]


def _check_homeassistant_configured(tool_name: str, ha_creds: dict[str, Any] | None) -> bool:
    """Check if HomeAssistant is configured."""
    if tool_name == "homeassistant":
        if not ha_creds:
            return False
        # Check for the fields that HomeAssistantTools actually uses
        has_url = "instance_url" in ha_creds
        has_token = "access_token" in ha_creds or "long_lived_token" in ha_creds
        return has_url and has_token
    return False


def _check_standard_tool_configured(tool: dict[str, Any], credentials: dict[str, Any] | None) -> bool:
    """Check if a standard tool with config_fields is configured."""
    if not tool.get("config_fields"):
        return False

    if not credentials:
        return False

    # Check if all required fields are present
    required_fields = [field["name"] for field in tool.get("config_fields", []) if field.get("required", True)]
    return all(field in credentials for field in required_fields)


def _append_config_only_presets(tools: list[dict[str, Any]]) -> None:
    """Append config-only tool presets so the dashboard can display them."""
    existing_tool_names = {tool.get("name") for tool in tools}
    for preset_name, expansion in Config.TOOL_PRESETS.items():
        if preset_name in existing_tool_names:
            continue
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


def _update_tools_statuses(
    tools: list[dict[str, Any]],
    request: Request,
    agent_name: str | None,
    *,
    worker_scope: WorkerScope | None,
) -> None:
    """Update tool availability using the resolved credential target."""
    if not dashboard_supports_worker_credentials(worker_scope):
        return

    target = resolve_request_credentials_target(request, agent_name=agent_name)
    credentials_cache: dict[str, dict[str, Any] | None] = {}

    def get_credentials(service: str) -> dict[str, Any] | None:
        if service not in credentials_cache:
            credentials_cache[service] = load_credentials_for_target(service, target)
        return credentials_cache[service]

    for tool in tools:
        tool_name = tool["name"]
        if tool.get("status") != "requires_config":
            continue

        auth_provider = tool.get("auth_provider")
        if auth_provider:
            provider_creds = get_credentials(auth_provider)
            if provider_creds and (
                (auth_provider == "google" and check_google_tool_configured(tool_name, provider_creds))
                or auth_provider != "google"
            ):
                tool["status"] = "available"
            continue

        if _check_homeassistant_configured(
            tool_name,
            get_credentials("homeassistant"),
        ) or _check_standard_tool_configured(tool, get_credentials(tool_name)):
            tool["status"] = "available"


@router.get("")
@router.get("/")
async def get_registered_tools(request: Request, agent_name: str | None = None) -> ToolsResponse:
    """Get all registered tools from mindroom.

    This builds tool metadata from the in-memory registry and updates availability
    based on credentials (including plugin-provided tools).
    """
    from mindroom.api.main import api_runtime_paths, load_runtime_config  # noqa: PLC0415

    runtime_paths = api_runtime_paths(request)
    config, _ = load_runtime_config(runtime_paths)
    ensure_tool_registry_loaded(runtime_paths, config)
    tools = export_tools_metadata()
    worker_scope = config.get_agent_worker_scope(agent_name) if agent_name in config.agents else None
    if not worker_scope_allows_shared_only_integrations(worker_scope):
        tools = [tool for tool in tools if tool["name"] not in SHARED_ONLY_INTEGRATION_NAMES]
    _append_config_only_presets(tools)
    _update_tools_statuses(tools, request, agent_name, worker_scope=worker_scope)

    return ToolsResponse(tools=tools)
