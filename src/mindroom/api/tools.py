"""API endpoints for tools information."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from mindroom.agent_policy import dashboard_credentials_supported_for_scope
from mindroom.api import config_lifecycle
from mindroom.api.dashboard_credential_scope import (
    require_agent_oauth_connection_authorized,
    resolve_dashboard_agent_execution_scope_request,
    resolve_dashboard_execution_scope_override,
)
from mindroom.config.models import lazy_loading_prohibited_reason
from mindroom.credentials.manager import (
    get_runtime_credentials_manager,
    load_scoped_credentials,
)
from mindroom.mcp.registry import with_catalog_function_names
from mindroom.oauth import OAuthProviderError
from mindroom.oauth.credential_lifecycle import (
    load_oauth_credentials_snapshot,
    oauth_credentials_usable,
    resolve_oauth_credential_context,
)
from mindroom.oauth.service import oauth_provider_service_account_configured
from mindroom.tool_system.catalog import export_tools_metadata
from mindroom.tool_system.plugins import ConfigPlugins, config_plugins
from mindroom.tool_system.sandbox_proxy import primary_owns_tool_settings
from mindroom.tool_system.worker_routing import (
    WorkerScope,
    build_worker_target_from_runtime_env,
    resolve_worker_target,
    unsupported_shared_only_integration_names,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.registry import ToolRegistry
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
    agent_name: str | None
    # Whether credential status is authoritative for this scope, which is also when the dashboard can configure it.
    status_authoritative: bool
    credentials_manager: CredentialsManager
    worker_target: ResolvedWorkerTarget | None
    allowed_shared_services: frozenset[str] | None
    oauth_providers: Mapping[str, OAuthProvider]
    runtime_paths: RuntimePaths
    tools: ToolRegistry
    oauth_config: Config | None = None


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


def _required_fields_stored(tool: dict[str, Any], credentials: dict[str, Any] | None) -> bool:
    """Return whether every required config field is present in credentials."""
    required_fields = [field["name"] for field in tool.get("config_fields") or [] if field.get("required", True)]
    return all(field in (credentials or {}) for field in required_fields)


def _check_standard_tool_configured(tool: dict[str, Any], credentials: dict[str, Any] | None) -> bool:
    """Check if a standard tool with config_fields is configured."""
    if not tool.get("config_fields") or not credentials:
        return False
    return _required_fields_stored(tool, credentials)


def _check_auth_provider_configured(
    tool: dict[str, Any],
    credentials: dict[str, Any] | None,
    *,
    provider: OAuthProvider | None,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether a delegated auth provider has usable credentials for one tool."""
    if provider is None:
        if not credentials:
            return False
        return _check_standard_tool_configured(tool, credentials)
    if oauth_provider_service_account_configured(provider, runtime_paths):
        return True
    if not credentials:
        return False
    return oauth_credentials_usable(provider, runtime_paths, credentials)


def _check_manual_oauth_fallback_configured(
    tool: dict[str, Any],
    credentials: dict[str, Any] | None,
) -> bool:
    """Return whether all declared manual OAuth fallback fields are configured."""
    fallback_fields = tool.get("oauth_fallback_fields")
    if not isinstance(fallback_fields, list | tuple) or not fallback_fields or not credentials:
        return False
    for field_name in fallback_fields:
        if not isinstance(field_name, str):
            return False
        value = credentials.get(field_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            return False
    return True


def _check_environment_oauth_fallback_configured(tool: dict[str, Any], runtime_paths: RuntimePaths) -> bool:
    """Return whether the environment sets every manual OAuth fallback field, the way the tool reads it."""
    fallback_fields = tool.get("oauth_fallback_fields")
    if not fallback_fields:
        return False
    env_by_field = {field["name"]: field["env"] for field in tool["config_fields"]}
    return all(
        runtime_paths.first_env_value(env_by_field.get(field_name, ())) is not None for field_name in fallback_fields
    )


def _annotate_tool_support(tools: list[dict[str, Any]], context: _ResolvedToolAvailabilityContext) -> None:
    """Expose, per tool, lazy-loading support, execution-scope support, and dashboard credential configuration."""
    unsupported_tools = set(
        unsupported_shared_only_integration_names([tool["name"] for tool in tools], context.execution_scope),
    )
    for tool in tools:
        tool["lazy_loading_supported"] = lazy_loading_prohibited_reason(tool["name"]) is None
        tool["execution_scope_supported"] = tool["name"] not in unsupported_tools
        tool["dashboard_configuration_supported"] = context.status_authoritative


def _resolve_tool_availability_context(
    request: Request,
    *,
    runtime_paths: RuntimePaths,
    config: Config,
    plugins: ConfigPlugins,
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
    authorized_identity = (
        require_agent_oauth_connection_authorized(
            request,
            config=config,
            runtime_paths=runtime_paths,
            agent_name=scope_request.agent_name,
        )
        if scope_request.agent_name is not None
        else None
    )
    execution_identity = (
        authorized_identity if not scope_request.draft_scope_preview and scope_request.agent_name is not None else None
    )
    worker_target = (
        build_worker_target_from_runtime_env(
            execution_scope,
            scope_request.agent_name,
            execution_identity=execution_identity,
            runtime_paths=runtime_paths,
        )
        if not scope_request.draft_scope_preview
        and (scope_request.agent_name is not None or execution_scope is not None)
        else None
    )
    return _ResolvedToolAvailabilityContext(
        execution_scope=execution_scope,
        agent_name=scope_request.agent_name,
        status_authoritative=status_authoritative,
        credentials_manager=get_runtime_credentials_manager(runtime_paths),
        worker_target=worker_target,
        allowed_shared_services=(config.get_worker_grantable_credentials() if execution_scope is not None else None),
        oauth_providers=plugins.oauth_providers,
        runtime_paths=runtime_paths,
        tools=plugins.tools,
        oauth_config=config,
    )


async def _load_oauth_provider_credentials(
    provider: OAuthProvider,
    context: _ResolvedToolAvailabilityContext,
    cache: dict[tuple[str, str | None], dict[str, Any] | None],
) -> dict[str, Any] | None:
    """Load one registered provider through the authoritative lifecycle owner."""
    credential_context = resolve_oauth_credential_context(
        provider,
        context.runtime_paths,
        context.credentials_manager,
        context.worker_target,
        config=context.oauth_config,
    )
    provider_target = credential_context.worker_target
    if provider.requester_scoped_credentials and provider_target is None:
        return None
    cache_key = (
        provider.id,
        provider_target.worker_key if provider_target is not None else None,
    )
    if cache_key not in cache:
        try:
            cache[cache_key] = (await load_oauth_credentials_snapshot(credential_context)).credentials
        except OAuthProviderError:
            cache[cache_key] = None
    return cache[cache_key]


def _tool_status_credentials(
    service: str,
    context: _ResolvedToolAvailabilityContext,
    *,
    authoritative: bool,
) -> dict[str, Any] | None:
    """Return the credentials the runtime would read for one service, or a preview of them without a requester.

    Dashboard users are not the trusted requester of a live Matrix sender, so a preview reads only what the
    requested scope, saved or draft, holds for no requester, and a service that lives only in a requester's store
    previews as unconfigured.
    """
    worker_target = context.worker_target
    if not authoritative:
        worker_target = (
            None
            if context.execution_scope is None
            else resolve_worker_target(context.execution_scope, context.agent_name, execution_identity=None)
        )
    try:
        return load_scoped_credentials(
            service,
            credentials_manager=context.credentials_manager,
            worker_target=worker_target,
            allowed_shared_services=context.allowed_shared_services,
            primary_built_tool=worker_target is not None
            and worker_target.routing_agent_name is not None
            and primary_owns_tool_settings(service, context.tools, runtime_paths=context.runtime_paths),
        )
    except ValueError:
        if authoritative:
            raise
        return None


async def _update_tools_statuses(
    tools: list[dict[str, Any]],
    context: _ResolvedToolAvailabilityContext,
) -> None:
    """Update tool runtime availability using the resolved credential context."""
    credentials_cache: dict[tuple[str, bool], dict[str, Any] | None] = {}
    oauth_credentials_cache: dict[tuple[str, str | None], dict[str, Any] | None] = {}

    def get_credentials(service: str, *, use_request_target: bool = False) -> dict[str, Any] | None:
        cache_key = (service, use_request_target)
        if cache_key not in credentials_cache:
            credentials_cache[cache_key] = _tool_status_credentials(
                service,
                context,
                authoritative=context.status_authoritative or use_request_target,
            )
        return credentials_cache[cache_key]

    for tool in tools:
        tool_name = tool["name"]
        if tool.get("status") != "requires_config":
            continue

        auth_provider = tool.get("auth_provider")
        if auth_provider:
            provider = context.oauth_providers.get(auth_provider)
            use_request_target = (
                provider is not None and provider.requester_scoped_credentials and context.worker_target is not None
            )
            manual_auth_configured = _check_manual_oauth_fallback_configured(
                tool,
                (
                    get_credentials(tool_name, use_request_target=use_request_target)
                    if tool.get("oauth_fallback_fields")
                    else None
                ),
            )
            if tool.get("oauth_fallback_fields"):
                tool["manual_auth_configured"] = manual_auth_configured
            environment_auth_configured = _check_environment_oauth_fallback_configured(tool, context.runtime_paths)
            tool["environment_auth_configured"] = environment_auth_configured
            credential_service = provider.credential_service if provider is not None else auth_provider
            provider_creds = (
                await _load_oauth_provider_credentials(provider, context, oauth_credentials_cache)
                if provider is not None and (context.status_authoritative or use_request_target)
                else get_credentials(credential_service, use_request_target=use_request_target)
            )
            auth_configured = (
                manual_auth_configured
                or environment_auth_configured
                or _check_auth_provider_configured(
                    tool,
                    provider_creds,
                    provider=provider,
                    runtime_paths=context.runtime_paths,
                )
            )
            # An OAuth connection alone cannot construct a tool whose own required settings are missing.
            # Without a registered OAuth provider, the required fields live in the provider credentials already checked above.
            if auth_configured and (provider is None or _required_fields_stored(tool, get_credentials(tool_name))):
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
    config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    plugins = config_plugins(config, runtime_paths)
    tools = export_tools_metadata(with_catalog_function_names(plugins.tools.listable()))
    execution_scope_override_provided, execution_scope_override = resolve_dashboard_execution_scope_override(request)
    context = _resolve_tool_availability_context(
        request,
        runtime_paths=runtime_paths,
        config=config,
        plugins=plugins,
        agent_name=agent_name,
        execution_scope_override_provided=execution_scope_override_provided,
        execution_scope_override=execution_scope_override,
    )
    _annotate_tool_support(tools, context)
    await _update_tools_statuses(tools, context)

    return ToolsResponse(tools=tools, status_authoritative=context.status_authoritative)
