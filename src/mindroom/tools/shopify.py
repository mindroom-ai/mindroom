"""Shopify tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

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
            description="Your Shopify store name (e.g., 'my-store' from my-store.myshopify.com).",
        ),
        ConfigField(
            name="access_token",
            label="Access Token",
            type="password",
            required=True,
            description="Shopify Admin API access token.",
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
    function_names=(
        "get_average_order_value",
        "get_customer_order_history",
        "get_inventory_levels",
        "get_low_stock_products",
        "get_order_analytics",
        "get_orders",
        "get_product_sales_breakdown",
        "get_products",
        "get_products_bought_together",
        "get_repeat_customers",
        "get_sales_by_date_range",
        "get_sales_trends",
        "get_shop_info",
        "get_top_selling_products",
    ),
)
def shopify_tools() -> type[ShopifyTools]:
    """Return Shopify tools for e-commerce analytics."""
    from agno.tools.shopify import ShopifyTools

    return ShopifyTools
