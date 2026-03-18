"""API endpoints for tools information."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from mindroom.agent_policy import dashboard_credentials_supported_for_scope
from mindroom.api import config_lifecycle
from mindroom.api.credentials import (
    build_dashboard_execution_identity,
    resolve_dashboard_agent_execution_scope_request,
    resolve_dashboard_execution_scope_override,
)
from mindroom.api.google_tools_helper import check_google_tool_configured
from mindroom.config.main import Config
from mindroom.credentials import get_runtime_credentials_manager, load_scoped_credentials
from mindroom.tool_system.metadata import ensure_tool_registry_loaded, export_tools_metadata
from mindroom.tool_system.worker_routing import (
    WorkerScope,
    build_worker_target_from_runtime_env,
    unsupported_shared_only_integration_names,
)

if TYPE_CHECKING:
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

router = APIRouter(prefix="/api/tools", tags=["tools"])


class ToolsResponse(BaseModel):
    """Response containing all registered tools."""

    tools: list[dict]
    status_authoritative: bool = True


@dataclass(frozen=True)
class _ResolvedToolAvailabilityContext:
    """Runtime tool-availability context for one dashboard request."""

    execution_scope: WorkerScope | None
    dashboard_configuration_supported: bool
    status_authoritative: bool
    credentials_manager: CredentialsManager
    worker_target: ResolvedWorkerTarget | None


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
                "dashboard_configuration_supported": True,
            },
        )


def _annotate_dashboard_configuration_support(
    tools: list[dict[str, Any]],
    *,
    supported: bool,
) -> None:
    """Expose whether dashboard credential configuration is supported for this scope."""
    for tool in tools:
        tool["dashboard_configuration_supported"] = supported


def _annotate_execution_scope_support(
    tools: list[dict[str, Any]],
    *,
    execution_scope: WorkerScope | None,
) -> None:
    """Expose whether each tool is supported for the requested execution scope."""
    unsupported_tools = set(
        unsupported_shared_only_integration_names([tool["name"] for tool in tools], execution_scope),
    )
    for tool in tools:
        tool["execution_scope_supported"] = tool["name"] not in unsupported_tools


def _load_env_shared_preview_credentials(
    service: str,
    *,
    credentials_manager: CredentialsManager,
) -> dict[str, Any] | None:
    """Return only env-backed shared credentials for non-authoritative dashboard previews.

    Dashboard users are not the same trusted requester identity as live Matrix senders.
    For isolated scopes we can still report capabilities and shared env-backed availability,
    but we must not pretend to inspect requester-owned scoped credential state.
    """
    shared_credentials = credentials_manager.shared_manager().load_credentials(service)
    if not isinstance(shared_credentials, Mapping):
        return None
    if shared_credentials.get("_source") != "env":
        return None
    return dict(shared_credentials)


def _resolve_tool_availability_context(
    request: Request,
    *,
    config: Config,
    agent_name: str | None,
    execution_scope_override_provided: bool,
    execution_scope_override: WorkerScope | None,
) -> _ResolvedToolAvailabilityContext:
    """Resolve one tool-availability context from persisted config plus optional draft override."""
    from mindroom.api.main import api_runtime_paths  # noqa: PLC0415

    scope_request = resolve_dashboard_agent_execution_scope_request(
        config=config,
        agent_name=agent_name,
        execution_scope_override_provided=execution_scope_override_provided,
        execution_scope_override=execution_scope_override,
        allow_draft_override=True,
    )
    execution_scope = scope_request.requested_execution_scope

    runtime_paths = api_runtime_paths(request)
    status_authoritative = not scope_request.draft_scope_preview and dashboard_credentials_supported_for_scope(
        execution_scope,
    )
    execution_identity = (
        build_dashboard_execution_identity(request, scope_request.agent_name)
        if status_authoritative and scope_request.agent_name is not None and execution_scope is not None
        else None
    )
    worker_target = (
        build_worker_target_from_runtime_env(
            execution_scope,
            scope_request.agent_name,
            execution_identity=execution_identity,
            runtime_paths=runtime_paths,
        )
        if status_authoritative and (scope_request.agent_name is not None or execution_scope is not None)
        else None
    )
    return _ResolvedToolAvailabilityContext(
        execution_scope=execution_scope,
        dashboard_configuration_supported=status_authoritative,
        status_authoritative=status_authoritative,
        credentials_manager=get_runtime_credentials_manager(runtime_paths),
        worker_target=worker_target,
    )


def _update_tools_statuses(
    tools: list[dict[str, Any]],
    context: _ResolvedToolAvailabilityContext,
) -> None:
    """Update tool runtime availability using the resolved credential context."""
    credentials_cache: dict[str, dict[str, Any] | None] = {}

    def get_credentials(service: str) -> dict[str, Any] | None:
        if service not in credentials_cache:
            if context.status_authoritative:
                credentials_cache[service] = load_scoped_credentials(
                    service,
                    credentials_manager=context.credentials_manager,
                    worker_target=context.worker_target,
                )
            else:
                credentials_cache[service] = _load_env_shared_preview_credentials(
                    service,
                    credentials_manager=context.credentials_manager,
                )
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
async def get_registered_tools(
    request: Request,
    agent_name: str | None = None,
) -> ToolsResponse:
    """Get all registered tools from mindroom.

    This builds tool metadata from the in-memory registry and updates availability
    based on credentials (including plugin-provided tools).
    """
    from mindroom.api.main import api_runtime_paths  # noqa: PLC0415

    runtime_paths = api_runtime_paths(request)
    config, _ = config_lifecycle.load_runtime_config(runtime_paths)
    ensure_tool_registry_loaded(runtime_paths, config)
    tools = export_tools_metadata()
    execution_scope_override_provided, execution_scope_override = resolve_dashboard_execution_scope_override(request)
    context = _resolve_tool_availability_context(
        request,
        config=config,
        agent_name=agent_name,
        execution_scope_override_provided=execution_scope_override_provided,
        execution_scope_override=execution_scope_override,
    )
    _append_config_only_presets(tools)
    _annotate_execution_scope_support(
        tools,
        execution_scope=context.execution_scope,
    )
    _annotate_dashboard_configuration_support(
        tools,
        supported=context.dashboard_configuration_supported,
    )
    _update_tools_statuses(tools, context)

    return ToolsResponse(tools=tools, status_authoritative=context.status_authoritative)
