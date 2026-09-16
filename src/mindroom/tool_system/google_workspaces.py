"""Register additional Google Workspace tools through an ordinary plugin.

Each workspace reuses the built-in toolkit implementations with distinct OAuth
providers, credential services and model-visible function names. Built-in tools
and their stored connections are never changed.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mindroom.oauth.google_calendar import google_calendar_oauth_provider
from mindroom.oauth.google_docs import google_docs_oauth_provider
from mindroom.oauth.google_drive import google_drive_oauth_provider
from mindroom.oauth.google_gmail import google_gmail_oauth_provider
from mindroom.oauth.google_sheets import google_sheets_oauth_provider
from mindroom.tool_system.catalog import TOOL_METADATA
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tool_system.toolkit_aliases import apply_toolkit_function_aliases

__all__ = ["GoogleWorkspaceConfig", "google_workspace_oauth_providers", "register_google_workspace_tools"]

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.tools import Toolkit

    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.declarations import ToolMetadata

_GoogleService = Literal["gmail", "google_calendar", "google_drive", "google_docs", "google_sheets"]
_PROVIDERS = {
    "gmail": google_gmail_oauth_provider,
    "google_calendar": google_calendar_oauth_provider,
    "google_drive": google_drive_oauth_provider,
    "google_docs": google_docs_oauth_provider,
    "google_sheets": google_sheets_oauth_provider,
}


class GoogleWorkspaceConfig(BaseModel):
    """Non-secret configuration for a separately connected Google workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,15}$")
    display_name: str = Field(min_length=1)
    client_config_service: str = Field(pattern=r"^[a-z][a-z0-9_]*_oauth_client$")
    allowed_hosted_domains: tuple[str, ...] = Field(min_length=1)
    services: tuple[_GoogleService, ...] = ("gmail", "google_calendar", "google_drive", "google_docs", "google_sheets")

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
        base = _PROVIDERS[service]()
        providers.append(
            replace(
                base,
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
                extra_auth_params={**base.extra_auth_params, "prompt": "consent select_account"},
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

        def initialize(toolkit: Toolkit, **kwargs: Any) -> None:  # noqa: ANN401
            base_class.__init__(toolkit, **kwargs)
            toolkit.name = tool_name
            aliases = {name: f"{prefix}_{name}" for name in {*toolkit.functions, *toolkit.async_functions}}
            apply_toolkit_function_aliases(toolkit, aliases)

        def oauth_only_kwargs(_self: object, _kwargs: dict[str, Any]) -> bool:
            # A workspace-specific OAuth tool must never inherit global delegated auth.
            return False

        def oauth_only_fallback(_self: object) -> bool:
            return False

        return type(
            f"{prefix}_{base_class.__name__}",
            (base_class,),
            {
                "__init__": initialize,
                "_oauth_provider": provider,
                "_oauth_tool_name": provider.tool_config_service,
                "_apply_runtime_original_auth_kwargs": oauth_only_kwargs,
                "_should_fallback_to_original_auth": oauth_only_fallback,
            },
        )

    return factory


def register_google_workspace_tools(workspace: GoogleWorkspaceConfig) -> None:
    """Register workspace tools from a plugin tools module, using its normal ownership."""
    import mindroom.tools  # noqa: F401, PLC0415

    for service, provider in zip(workspace.services, google_workspace_oauth_providers(workspace), strict=True):
        base = TOOL_METADATA[service]
        assert provider.tool_config_service is not None
        register_tool_with_metadata(
            name=provider.tool_config_service,
            display_name=provider.display_name,
            description=f"{base.description} using the connected {workspace.display_name} account",
            category=base.category,
            status=base.status,
            setup_type=base.setup_type,
            default_execution_target=base.default_execution_target,
            consumes_workspace_paths=base.consumes_workspace_paths,
            requires_room_context=base.requires_room_context,
            requires_primary_runtime=True,
            icon=base.icon,
            icon_color=base.icon_color,
            config_fields=base.config_fields,
            agent_override_fields=base.agent_override_fields,
            authored_override_validator=base.authored_override_validator,
            dependencies=base.dependencies,
            auth_provider=provider.id,
            oauth_fallback_fields=base.oauth_fallback_fields,
            docs_url=base.docs_url,
            helper_text=base.helper_text,
            function_names=tuple(f"{workspace.name}_{name}" for name in base.function_names),
            managed_init_args=base.managed_init_args,
            supports_toolkit_filters=base.supports_toolkit_filters,
        )(_workspace_tool_factory(base, provider, workspace.name))
