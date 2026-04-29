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
from mindroom.credentials import (
    get_runtime_credentials_manager,
    load_scoped_credentials,
    load_worker_grantable_shared_credentials,
)
from mindroom.oauth.registry import load_oauth_providers
from mindroom.tool_system.catalog import export_tools_metadata, resolved_tool_metadata_for_runtime
from mindroom.tool_system.worker_routing import (
    WorkerScope,
    build_worker_target_from_runtime_env,
    local_shared_credential_allowlist,
    unsupported_shared_only_integration_names,
)

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
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
    allowed_shared_services: frozenset[str] | None
    auth_provider_credential_services: dict[str, str]


def _effective_allowed_shared_services(
    service: str,
    context: _ResolvedToolAvailabilityContext,
) -> frozenset[str] | None:
    """Return the worker allowlist that applies to one dashboard credential lookup."""
    local_allowlist = local_shared_credential_allowlist(service, context.execution_scope)
    if local_allowlist is not None:
        return local_allowlist
    return context.allowed_shared_services


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


def _check_auth_provider_configured(
    tool_name: str,
    auth_provider: str,
    credentials: dict[str, Any] | None,
) -> bool:
    """Return whether a delegated auth provider has usable credentials for one tool."""
    if not credentials:
        return False
    if auth_provider == "google":
        return check_google_tool_configured(tool_name, credentials)
    return bool(credentials.get("token") or credentials.get("access_token") or credentials.get("refresh_token"))


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
                "agent_override_fields": None,
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


def _load_shared_preview_credentials(
    service: str,
    *,
    credentials_manager: CredentialsManager,
    allowed_shared_services: frozenset[str] | None,
) -> dict[str, Any] | None:
    """Return the shared credentials visible to non-authoritative dashboard previews.

    Dashboard users are not the same trusted requester identity as live Matrix senders.
    For isolated scopes we can still report capabilities and allowlisted shared availability,
    but we must not pretend to inspect requester-owned scoped credential state.
    """
    shared_manager = credentials_manager.shared_manager()
    if allowed_shared_services is None:
        shared_credentials = shared_manager.load_credentials(service)
        return dict(shared_credentials) if isinstance(shared_credentials, Mapping) else None
    return load_worker_grantable_shared_credentials(
        service,
        shared_manager=shared_manager,
        allowed_services=allowed_shared_services,
    )


def _resolve_tool_availability_context(
    request: Request,
    *,
    runtime_paths: RuntimePaths,
    config: Config,
    agent_name: str | None,
    execution_scope_override_provided: bool,
    execution_scope_override: WorkerScope | None,
) -> _ResolvedToolAvailabilityContext:
    """Resolve one tool-availability context from persisted config plus optional draft override."""
    scope_request = resolve_dashboard_agent_execution_scope_request(
        config=config,
        agent_name=agent_name,
        execution_scope_override_provided=execution_scope_override_provided,
        execution_scope_override=execution_scope_override,
        allow_draft_override=True,
    )
    execution_scope = scope_request.requested_execution_scope

    status_authoritative = not scope_request.draft_scope_preview and dashboard_credentials_supported_for_scope(
        execution_scope,
    )
    execution_identity = (
        build_dashboard_execution_identity(
            request,
            scope_request.agent_name,
            runtime_paths=runtime_paths,
        )
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
        allowed_shared_services=(config.get_worker_grantable_credentials() if execution_scope is not None else None),
        auth_provider_credential_services={
            provider_id: provider.credential_service
            for provider_id, provider in load_oauth_providers(config, runtime_paths).items()
        },
    )


def _read_tools_runtime_config(request: Request) -> tuple[Config, RuntimePaths]:
    """Read one coherent config/runtime snapshot for the tools dashboard route."""
    return config_lifecycle.read_committed_runtime_config(request)


def _update_tools_statuses(
    tools: list[dict[str, Any]],
    context: _ResolvedToolAvailabilityContext,
) -> None:
    """Update tool runtime availability using the resolved credential context."""
    credentials_cache: dict[str, dict[str, Any] | None] = {}

    def get_credentials(service: str) -> dict[str, Any] | None:
        if service not in credentials_cache:
            allowed_shared_services = _effective_allowed_shared_services(service, context)
            if context.status_authoritative:
                credentials_cache[service] = load_scoped_credentials(
                    service,
                    credentials_manager=context.credentials_manager,
                    worker_target=context.worker_target,
                    allowed_shared_services=allowed_shared_services,
                )
            else:
                credentials_cache[service] = _load_shared_preview_credentials(
                    service,
                    credentials_manager=context.credentials_manager,
                    allowed_shared_services=allowed_shared_services,
                )
        return credentials_cache[service]

    for tool in tools:
        tool_name = tool["name"]
        if tool.get("status") != "requires_config":
            continue

        auth_provider = tool.get("auth_provider")
        if auth_provider:
            credential_service = context.auth_provider_credential_services.get(auth_provider, auth_provider)
            provider_creds = get_credentials(credential_service)
            if _check_auth_provider_configured(tool_name, auth_provider, provider_creds):
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
    config, runtime_paths = _read_tools_runtime_config(request)
    tool_metadata = resolved_tool_metadata_for_runtime(
        runtime_paths,
        config,
        tolerate_plugin_load_errors=True,
    )
    tools = export_tools_metadata(tool_metadata)
    execution_scope_override_provided, execution_scope_override = resolve_dashboard_execution_scope_override(request)
    context = _resolve_tool_availability_context(
        request,
        runtime_paths=runtime_paths,
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
