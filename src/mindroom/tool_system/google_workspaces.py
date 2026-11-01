"""Register additional Google Workspace tools through an ordinary plugin.

Each workspace reuses the built-in toolkit implementations with distinct OAuth
providers, credential services and model-visible function names. Built-in tools
and their stored connections are never changed.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mindroom.oauth.google import google_oauth_providers, with_google_account_picker
from mindroom.tool_system.registry import builtin_tool_registry, register_tool

__all__ = ["GoogleWorkspaceConfig", "google_workspace_oauth_providers", "register_google_workspace_tools"]

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.agno_toolkits import AnyToolkit
    from mindroom.tool_system.declarations import ToolMetadata

_GoogleService = Literal["gmail", "google_calendar", "google_drive", "google_docs", "google_sheets", "google_tasks"]
_PROVIDERS = {
    provider.tool_config_service: provider
    for provider in google_oauth_providers()
    if provider.tool_config_service in get_args(_GoogleService)
}


class GoogleWorkspaceConfig(BaseModel):
    """Non-secret configuration for a separately connected Google workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,15}$")
    display_name: str = Field(min_length=1)
    client_config_service: str = Field(pattern=r"^[a-z][a-z0-9_]*_oauth_client$")
    allowed_hosted_domains: tuple[str, ...] = Field(min_length=1)
    services: tuple[_GoogleService, ...] = get_args(_GoogleService)

    @field_validator("services")
    @classmethod
    def unique_services(cls, services: tuple[_GoogleService, ...]) -> tuple[_GoogleService, ...]:
        """Reject empty or duplicate tool declarations before registering anything."""
        if not services or len(set(services)) != len(services):
            msg = "services must contain at least one service, without duplicates"
            raise ValueError(msg)
        return services

    @field_validator("allowed_hosted_domains")
    @classmethod
    def normalize_domains(cls, domains: tuple[str, ...]) -> tuple[str, ...]:
        """Require concrete hosted domains, as Google identity checks use exact matches."""
        if any(not domain or "*" in domain or "@" in domain or "/" in domain for domain in domains):
            msg = "allowed_hosted_domains must contain concrete domain names"
            raise ValueError(msg)
        return tuple(dict.fromkeys(domain.lower() for domain in domains))


def google_workspace_oauth_providers(workspace: GoogleWorkspaceConfig) -> tuple[OAuthProvider, ...]:
    """Build independent providers for a plugin's OAuth registration callback."""
    providers = []
    for service in workspace.services:
        base = _PROVIDERS[service]
        providers.append(
            replace(
                with_google_account_picker(base),
                id=f"{workspace.name}_{base.id}",
                display_name=f"{workspace.display_name} {base.display_name}",
                credential_service=f"{workspace.name}_{base.credential_service}",
                tool_config_service=f"{workspace.name}_{service}",
                client_config_services=(),
                shared_client_config_services=(workspace.client_config_service,),
                runtime_bootstrapper=None,
                allowed_email_domains_env=None,
                allowed_hosted_domains_env=None,
                allowed_hosted_domains=workspace.allowed_hosted_domains,
            ),
        )
    return tuple(providers)


def _workspace_tool_factory(
    metadata: ToolMetadata,
    provider: OAuthProvider,
    prefix: str,
) -> Callable[[], type]:
    """Keep toolkit imports lazy, like the built-in tool registry."""
    assert provider.tool_config_service is not None
    tool_name = provider.tool_config_service

    def factory() -> type:
        assert metadata.factory is not None
        base_class = metadata.factory()

        def initialize(toolkit: AnyToolkit, **kwargs: Any) -> None:  # noqa: ANN401
            # Deferred: the toolkit adapter loads Agno, which plugin code importing this module must not load.
            from mindroom.tool_system.agno_toolkits import (  # noqa: PLC0415
                rename_toolkit_functions,
                toolkit_function_names,
            )

            base_class.__init__(toolkit, **kwargs)
            toolkit.name = tool_name
            rename_toolkit_functions(toolkit, {name: f"{prefix}_{name}" for name in toolkit_function_names(toolkit)})

        def oauth_only_kwargs(_self: object, _kwargs: dict[str, Any]) -> bool:
            # A workspace-specific OAuth tool must never inherit global delegated auth.
            return False

        return type(
            f"{prefix}_{base_class.__name__}",
            (base_class,),
            {
                "__init__": initialize,
                "_oauth_provider": provider,
                "_oauth_tool_name": provider.tool_config_service,
                "_apply_runtime_original_auth_kwargs": oauth_only_kwargs,
            },
        )

    return factory


def register_google_workspace_tools(workspace: GoogleWorkspaceConfig) -> None:
    """Register workspace tools from a plugin tools module, using its normal ownership."""
    builtins = builtin_tool_registry().entries
    for service, provider in zip(workspace.services, google_workspace_oauth_providers(workspace), strict=True):
        base = builtins[service]
        assert provider.tool_config_service is not None
        register_tool(
            replace(
                base,
                name=provider.tool_config_service,
                display_name=provider.display_name,
                description=f"{base.description} using the connected {workspace.display_name} account",
                requires_primary_runtime=True,
                dependency_extra=service,
                auth_provider=provider.id,
                function_names=tuple(f"{workspace.name}_{name}" for name in base.function_names),
                factory=_workspace_tool_factory(base, provider, workspace.name),
            ),
        )
