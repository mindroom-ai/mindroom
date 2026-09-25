"""Register additional, separately connected Atlassian Cloud sites through an ordinary plugin.

Each connection gets its own OAuth provider, stored credentials, site pin, and
prefixed function names. The built-in ``atlassian`` tool and its stored
connections are never changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mindroom.oauth.atlassian import (
    ATLASSIAN_PRODUCTS,
    AtlassianProduct,
    atlassian_function_names,
    atlassian_oauth_provider,
    normalize_cloud_id,
    normalize_site_url,
)
from mindroom.tool_system.catalog import TOOL_METADATA
from mindroom.tool_system.registration import register_tool_with_metadata

__all__ = ["AtlassianConnectionConfig", "atlassian_connection_oauth_provider", "register_atlassian_connection_tools"]

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.custom_tools.atlassian import AtlassianToolkit
    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


class AtlassianConnectionConfig(BaseModel):
    """Non-secret configuration for one separately connected Atlassian Cloud site."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,15}$")
    display_name: str = Field(min_length=1)
    site_url: str | None = None
    cloud_id: str | None = None
    products: tuple[AtlassianProduct, ...] = ATLASSIAN_PRODUCTS
    write: bool = True
    client_config_service: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*_oauth_client$")

    @field_validator("site_url")
    @classmethod
    def canonical_site_url(cls, site_url: str | None) -> str | None:
        """Keep only the HTTPS origin, which is what Atlassian reports for each accessible site."""
        return normalize_site_url(site_url) if site_url else None

    @field_validator("cloud_id")
    @classmethod
    def canonical_cloud_id(cls, cloud_id: str | None) -> str | None:
        """Require a UUID, since the cloud ID becomes part of every gateway path."""
        return normalize_cloud_id(cloud_id) if cloud_id else None

    @field_validator("products")
    @classmethod
    def unique_products(cls, products: tuple[AtlassianProduct, ...]) -> tuple[AtlassianProduct, ...]:
        """Reject empty or duplicate product declarations before registering anything."""
        if not products or len(set(products)) != len(products):
            msg = "products must contain at least one of jira and confluence, without duplicates"
            raise ValueError(msg)
        return products

    @model_validator(mode="after")
    def pinned_site(self) -> AtlassianConnectionConfig:
        """Require a site pin, so a connection never acts on whichever site an account happens to reach."""
        if self.site_url is None and self.cloud_id is None:
            msg = "an Atlassian connection requires site_url or cloud_id"
            raise ValueError(msg)
        return self

    @property
    def provider_id(self) -> str:
        """Return the provider ID, which is also this connection's tool name and settings service."""
        return f"{self.name}_atlassian"

    @property
    def oauth_client_service(self) -> str:
        """Return the service holding this connection's Atlassian app credentials.

        Each connection defaults to its own app, since an Atlassian app may accept only one callback URL.
        """
        return self.client_config_service or f"{self.provider_id}_oauth_client"


def atlassian_connection_oauth_provider(connection: AtlassianConnectionConfig) -> OAuthProvider:
    """Build this connection's provider for a plugin's OAuth registration callback."""
    return atlassian_oauth_provider(
        provider_id=connection.provider_id,
        display_name=connection.display_name,
        products=connection.products,
        write=connection.write,
        client_config_service=connection.oauth_client_service,
    )


def _connection_tool_factory(connection: AtlassianConnectionConfig) -> Callable[[], type[AtlassianToolkit]]:
    """Keep toolkit imports lazy, like the built-in tool registry."""

    def factory() -> type[AtlassianToolkit]:
        from mindroom.custom_tools.atlassian import AtlassianToolkit  # noqa: PLC0415

        class AtlassianConnectionTools(AtlassianToolkit):
            def __init__(
                self,
                *,
                runtime_paths: RuntimePaths,
                credentials_manager: CredentialsManager | None = None,
                worker_target: ResolvedWorkerTarget | None = None,
                runtime_config: Config | None = None,
            ) -> None:
                super().__init__(
                    provider=atlassian_connection_oauth_provider(connection),
                    function_prefix=f"{connection.name}_",
                    products=connection.products,
                    write=connection.write,
                    site_url=connection.site_url,
                    cloud_id=connection.cloud_id,
                    runtime_paths=runtime_paths,
                    credentials_manager=credentials_manager,
                    worker_target=worker_target,
                    runtime_config=runtime_config,
                )

        AtlassianConnectionTools.__name__ = f"{connection.name}_AtlassianConnectionTools"
        return AtlassianConnectionTools

    return factory


def register_atlassian_connection_tools(connection: AtlassianConnectionConfig) -> None:
    """Register one connection's tool from a plugin tools module, using its normal ownership."""
    import mindroom.tools  # noqa: F401, PLC0415

    base = TOOL_METADATA["atlassian"]
    products = " and ".join(product.capitalize() for product in connection.products)
    register_tool_with_metadata(
        name=connection.provider_id,
        display_name=connection.display_name,
        description=f"{products} on {connection.site_url or connection.cloud_id} as the connected user",
        category=base.category,
        status=base.status,
        setup_type=base.setup_type,
        file_access=base.file_access,
        requires_primary_runtime=True,
        auth_provider=connection.provider_id,
        icon=base.icon,
        icon_color=base.icon_color,
        managed_init_args=base.managed_init_args,
        docs_url=base.docs_url,
        helper_text=(
            f"Each user connects their own {connection.display_name} account. "
            f"Store the Atlassian OAuth 2.0 (3LO) app client ID and secret in {connection.oauth_client_service}."
        ),
        function_names=atlassian_function_names(
            connection.products,
            write=connection.write,
            prefix=f"{connection.name}_",
        ),
    )(_connection_tool_factory(connection))
