"""Shopify tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.shopify import ShopifyTools


@register_tool_with_metadata(
    name="shopify",
    display_name="Shopify",
    description="Analyze sales data, products, orders, and customer insights from your Shopify store",
    category=ToolCategory.INTEGRATIONS,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiShopify",
    icon_color="text-green-600",
    config_fields=[
        ConfigField(
            name="shop_name",
            label="Shop Name",
            type="text",
            required=True,
            placeholder="my-store",
            description="Your Shopify store name (e.g., 'my-store' from my-store.myshopify.com). Falls back to SHOPIFY_SHOP_NAME env var.",
        ),
        ConfigField(
            name="access_token",
            label="Access Token",
            type="password",
            required=True,
            description="Shopify Admin API access token. Falls back to SHOPIFY_ACCESS_TOKEN env var.",
        ),
        ConfigField(
            name="api_version",
            label="API Version",
            type="text",
            required=False,
            default="2025-10",
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://docs.agno.com/tools/toolkits/others/shopify",
    helper_text="Create a custom app in your [Shopify Admin](https://admin.shopify.com/) to get an access token",
)
def shopify_tools() -> type[ShopifyTools]:
    """Return Shopify tools for e-commerce analytics."""
    from agno.tools.shopify import ShopifyTools

    return ShopifyTools
